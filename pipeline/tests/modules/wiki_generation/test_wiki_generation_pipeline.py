import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock, patch

from run_lab import (
    PipelineLog,
    PipelinePrompts,
    _assemble_wiki_pages,
    _extract_pipeline_source,
    _load_pipeline_prompts,
    _prepare_post_ingest_clusters,
    _resolve_pipeline_concepts,
    _run_wiki_generation_loop,
)


@dataclass
class FakeDocument:
    document_id: str
    title: str


@dataclass
class FakeBlock:
    block_id: str
    text: str


class FakeNormalizer:
    def normalize_notes(self, notes: list[dict[str, object]]) -> dict[str, object]:
        return {
            "attempt": notes[0]["attempt"],
            "concept_ledger": [],
            "evidence_units": [],
        }


class FakeConceptResolutionClient:
    def __init__(self) -> None:
        self.calls = 0
        self.system_prompts: list[str] = []
        self.barrier = Barrier(2)

    def complete_json(self, system_prompt: str, _user_prompt: str) -> dict[str, object]:
        self.calls += 1
        self.system_prompts.append(system_prompt)
        self.barrier.wait(timeout=2)
        if "ConceptUpdateCandidateJudge" in system_prompt:
            return {
                "decisions": [
                    {
                        "candidate_id": "cand_001",
                        "decision": "same_concept",
                        "concept_slug": "concept-a",
                    }
                ]
            }
        return {
            "resolutions": [],
            "hint_resolutions": [],
        }


class WikiGenerationPipelineTest(unittest.TestCase):
    def test_api_page_assembly_preserves_content_without_llm_calls(self) -> None:
        client = Mock()
        normalized = {
            "document": {"document_id": "doc-1", "title": "문서", "source_path": "document.md"},
            "semantic_notes": [{
                "semantic_summary": "추출한 요약",
                "key_points": [{"text": "추출한 핵심", "anchor_reference_ids": ["B0001"]}],
            }],
            "concept_ledger": [{
                "slug": "concept-a", "title": "개념 A", "definition": "추출한 정의",
                "display_reference_ids": ["B0001"], "source_document_ids": ["doc-1"],
            }],
            "evidence_units": [{
                "claim": "추출한 근거", "related_concept_slugs": ["concept-a"],
                "anchor_reference_ids": ["B0001"], "source_document_id": "doc-1",
            }],
            "warnings": [],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            outputs = _assemble_wiki_pages(
                SimpleNamespace(mode="api", source_page_mode="auto", concept_page_mode="auto",
                                selection_mode=None, save_debug_json=False),
                api_client=client,
                prompts=PipelinePrompts("", "", "", "", ""),
                normalized=normalized, blocks=[], existing_source_artifact=None,
                existing_source_markdown=None, out=Path(tmp_dir),
                log=PipelineLog(Path(tmp_dir) / "pipeline.log"),
            )
        self.assertEqual(client.mock_calls, [])
        self.assertEqual(outputs.source_page_mode, "skeleton")
        self.assertEqual(outputs.concept_page_mode, "skeleton")
        self.assertIn("추출한 요약", outputs.source_page["markdown"])
        self.assertEqual(outputs.source_page["source_extraction_artifact"]["key_points"],
                         [{"text": "추출한 핵심", "evidence_block_ids": ["B0001"]}])
        for text in ("추출한 정의", "추출한 핵심", "추출한 근거", "B0001"):
            self.assertIn(text, outputs.concept_pages[0]["markdown"])

    def test_prepare_post_ingest_clusters_defers_cluster_judge(self) -> None:
        normalized = {
            "document": {"document_id": "doc-1"},
            "section_candidates": [
                {
                    "term": "Concept A",
                    "context": "Concept A를 보강한다.",
                    "anchor_reference_ids": ["B0001"],
                }
            ],
            "mentions": [],
            "unresolved_related_concept_hints": [],
            "evidence_units": [],
            "concept_update_decisions": [
                {
                    "candidate_id": "cand_001",
                    "decision": "same_concept",
                    "concept_slug": "concept-a",
                    "relation": "same_concept",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            initial, post_ingest = _prepare_post_ingest_clusters(
                normalized=normalized,
                log=PipelineLog(Path(tmp_dir) / "pipeline.log"),
            )

        self.assertEqual(
            initial["concept_update_decisions"][0]["claim"],
            "Concept A - Concept A를 보강한다.",
        )
        self.assertEqual(
            post_ingest["cluster_normalized"]["document"],
            {"document_id": "doc-1"},
        )

    def test_assemble_wiki_pages_keeps_skeleton_modes_without_api(self) -> None:
        normalized = {
            "document": {
                "document_id": "doc-1",
                "title": "문서",
                "source_path": "document.md",
            },
            "semantic_notes": [],
            "concept_ledger": [],
            "evidence_units": [],
            "warnings": [],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "pipeline.log"
            outputs = _assemble_wiki_pages(
                SimpleNamespace(
                    mode="offline",
                    source_page_mode="auto",
                    concept_page_mode="auto",
                    selection_mode=None,
                    save_debug_json=False,
                ),
                api_client=None,
                prompts=PipelinePrompts("", "", "", "", ""),
                normalized=normalized,
                blocks=[],
                existing_source_artifact=None,
                existing_source_markdown=None,
                out=Path(tmp_dir),
                log=PipelineLog(log_path),
            )
            log_text = log_path.read_text(encoding="utf-8")

        self.assertEqual(outputs.source_page_mode, "skeleton")
        self.assertEqual(outputs.concept_page_mode, "skeleton")
        self.assertEqual(outputs.source_page["title"], "문서")
        self.assertEqual(outputs.concept_pages, [])
        self.assertEqual(outputs.links, [])
        self.assertLess(
            log_text.index("[5-보조. Concept 입력 준비]"),
            log_text.index("[5. Source Page 생성]"),
        )
        self.assertLess(
            log_text.index("[5. Source Page 생성]"),
            log_text.index("[6. Concept Page 생성]"),
        )

    def test_load_pipeline_prompts_returns_named_prompt_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            prompt_paths = {}
            for name in (
                "system_prompt",
                "concept_system_prompt",
                "concept_resolution_system_prompt",
                "wiki_evaluator_system_prompt",
                "wiki_patch_system_prompt",
            ):
                path = Path(tmp_dir) / f"{name}.md"
                path.write_text(name, encoding="utf-8")
                prompt_paths[name] = str(path)

            prompts = _load_pipeline_prompts(
                SimpleNamespace(**prompt_paths),
                PipelineLog(Path(tmp_dir) / "pipeline.log"),
            )

        self.assertEqual(prompts.semantic, "system_prompt")
        self.assertEqual(prompts.concept, "concept_system_prompt")
        self.assertEqual(prompts.wiki_patch, "wiki_patch_system_prompt")

    def test_extract_pipeline_source_applies_requested_document_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            document, blocks, source_block_records = _extract_pipeline_source(
                SimpleNamespace(
                    selection_mode=None,
                    source_document_id="requested-document",
                    save_debug_json=False,
                ),
                input_text="# 문서\n\n본문입니다.",
                input_source_name="inline.md",
                input_path=Path("inline.md"),
                out=Path(tmp_dir),
                log=PipelineLog(Path(tmp_dir) / "pipeline.log"),
            )

        self.assertEqual(document.document_id, "requested-document")
        self.assertTrue(blocks)
        self.assertTrue(all(block.document_id == "requested-document" for block in blocks))
        self.assertEqual(source_block_records[0]["document_id"], "requested-document")

    def test_resolve_pipeline_concepts_preserves_resolution_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            client = FakeConceptResolutionClient()
            log_path = Path(tmp_dir) / "pipeline.log"
            normalized = _resolve_pipeline_concepts(
                SimpleNamespace(
                    existing_concept_index=[],
                    existing_wiki_dir=None,
                    save_debug_json=False,
                ),
                api_client=client,  # type: ignore[arg-type]
                concept_resolution_prompt="resolve",
                normalized={
                    "concept_ledger": [
                        {
                            "slug": "concept-a",
                            "title": "Concept A",
                            "aliases": [],
                            "anchor_reference_ids": ["B0001"],
                        }
                    ],
                    "document": {"document_id": "doc-1"},
                    "section_candidates": [
                        {
                            "term": "Concept A",
                            "context": "Concept A를 보강한다.",
                            "anchor_reference_ids": ["B0001"],
                        }
                    ],
                    "mentions": [],
                    "evidence_units": [],
                    "missing_related_concept_hints": [],
                    "warnings": [],
                },
                out=Path(tmp_dir),
                log=PipelineLog(log_path),
            )
            log_text = log_path.read_text(encoding="utf-8")

        self.assertEqual(normalized["concept_ledger"][0]["slug"], "concept-a")
        self.assertEqual(normalized["concept_resolutions"][0]["decision"], "create_new")
        self.assertEqual(
            normalized["concept_update_decisions"][0]["concept_slug"],
            "concept-a",
        )
        self.assertEqual(client.calls, 2)
        self.assertTrue(
            any("ConceptUpdateCandidateJudge" in prompt for prompt in client.system_prompts)
        )
        self.assertIn("resolution 소요 시간(초)", log_text)
        self.assertIn("update judge 소요 시간(초)", log_text)
        self.assertIn("병렬 wall 시간(초)", log_text)
        self.assertIn("병렬 절감 시간(초)", log_text)

    def test_evaluator_feedback_retries_semantic_extraction_until_passed(self) -> None:
        prompts: list[str] = []
        evaluations = [
            {
                "passed": False,
                "retry_recommended": True,
                "retry_feedback": "누락된 source anchor를 보강하세요.",
                "scores": {"overall": 0.4},
                "issues": [],
            },
            {
                "passed": True,
                "retry_recommended": False,
                "retry_feedback": "",
                "scores": {"overall": 0.95},
                "issues": [],
            },
        ]

        def fake_semantic_extraction(
            _self,
            system_prompt,
            attempt,
            _source_context,
            _previous_notes=None,
            _target_block_ids=None,
        ):
            prompts.append(system_prompt)
            return [{"attempt": attempt}]

        def fake_evaluate_generation(_self, _normalized):
            return evaluations.pop(0)

        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch("run_lab.SemanticGenerationAdapter.generate", new=fake_semantic_extraction):
                with patch("run_lab.GenerationEvaluatorAdapter.evaluate", new=fake_evaluate_generation):
                    notes, normalized, generation_evaluations = _run_wiki_generation_loop(
                        api_client=SimpleNamespace(),
                        semantic_system_prompt="기본 semantic prompt",
                        wiki_evaluator_system_prompt="evaluator prompt",
                        packets=[SimpleNamespace(chunk_id="chunk-1", block_ids=["B0001"])],
                        raw_dir=None,
                        log=PipelineLog(Path(tmp_dir) / "pipeline.log"),
                        normalizer=FakeNormalizer(),
                        document=FakeDocument(document_id="doc-1", title="테스트 문서"),
                        blocks=[FakeBlock(block_id="B0001", text="본문")],
                        out=Path(tmp_dir),
                        save_debug_json=False,
                        wiki_evaluation_loop=True,
                        max_eval_attempts=2,
                    )

        self.assertEqual([note["attempt"] for note in notes], [2])
        self.assertEqual(normalized["attempt"], 2)
        self.assertEqual(len(generation_evaluations), 2)
        self.assertEqual(len(prompts), 2)
        self.assertIn("누락된 source anchor를 보강하세요.", prompts[1])


if __name__ == "__main__":
    unittest.main()
