from __future__ import annotations

import re
from datetime import date
from hashlib import sha1
from pathlib import Path
from typing import Any

from app.modules.wiki_generation.domain.text_utils import slugify, unique_keep_order
from app.modules.wiki_generation.infrastructure.meaning_cluster_log import render_meaning_cluster_log
from app.modules.wiki_generation.infrastructure.ref_format import cite_global_refs, global_refs
from app.modules.wiki_ingestion.infrastructure.file_io import write_text


class MeaningClusterArtifactAssembler:
    """Build active cluster and ingest log markdown artifacts from normalized output."""

    def build(
        self,
        normalized: dict[str, Any],
        user_id: str,
        workspace_id: str,
        cluster_decisions: list[dict[str, Any]] | None = None,
        concept_update_decisions: list[dict[str, Any]] | None = None,
        core_relation_decisions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        concept_update_decisions = concept_update_decisions or []
        core_relation_decisions = core_relation_decisions or []
        clusters = self._clusters(normalized, cluster_decisions or [], concept_update_decisions, core_relation_decisions)
        invalid_candidates = self.invalid_candidate_claims(normalized)
        active_markdown = self._active_markdown(clusters)
        maintenance_summary = self._maintenance_summary(clusters, invalid_candidates)
        ingest_date = date.today().isoformat()
        log_markdown = render_meaning_cluster_log(
            normalized,
            clusters,
            user_id,
            workspace_id,
            concept_update_decisions,
            invalid_candidates,
            ingest_date,
        )
        return {
            "active_path": f"wiki/{user_id}/{workspace_id}/clusters/active.md",
            "log_path": f"wiki/{user_id}/{workspace_id}/logs/{ingest_date}.md",
            "active_markdown": active_markdown,
            "log_markdown": log_markdown,
            "clusters": clusters,
            "concept_update_decisions": concept_update_decisions,
            "core_relation_decisions": core_relation_decisions,
            "invalid_candidates": invalid_candidates,
            "maintenance_summary": maintenance_summary,
        }

    def assemble(
        self,
        normalized: dict[str, Any],
        out_dir: str | Path,
        user_id: str,
        workspace_id: str,
        cluster_decisions: list[dict[str, Any]] | None = None,
        concept_update_decisions: list[dict[str, Any]] | None = None,
        core_relation_decisions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        artifact = self.build(
            normalized,
            user_id=user_id,
            workspace_id=workspace_id,
            cluster_decisions=cluster_decisions,
            concept_update_decisions=concept_update_decisions,
            core_relation_decisions=core_relation_decisions,
        )
        write_text(Path(out_dir) / artifact["active_path"], artifact["active_markdown"])
        write_text(Path(out_dir) / artifact["log_path"], artifact["log_markdown"])
        return artifact

    def candidate_claims(self, normalized: dict[str, Any]) -> list[dict[str, Any]]:
        return [candidate for candidate in self._candidate_claims(normalized) if candidate.get("refs")]

    def invalid_candidate_claims(self, normalized: dict[str, Any]) -> list[dict[str, Any]]:
        invalid = []
        for candidate in self._candidate_claims(normalized):
            if candidate.get("refs"):
                continue
            invalid.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "claim_id": candidate["claim_id"],
                    "term": candidate["term"],
                    "slug": candidate["slug"],
                    "claim": candidate["claim"],
                    "candidate_type": candidate["candidate_type"],
                    "reason": "missing source refs",
                }
            )
        return invalid

    def _candidate_claims(self, normalized: dict[str, Any]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        evidence_by_id = {item.get("evidence_id"): item for item in normalized.get("evidence_units", [])}
        for item in normalized.get("section_candidates", []):
            candidate = self._source_candidate(normalized, item, "section", len(candidates) + 1)
            if candidate:
                candidates.append(candidate)
        for item in normalized.get("mentions", []):
            candidate = self._source_candidate(normalized, item, "mention", len(candidates) + 1)
            if candidate:
                candidates.append(candidate)
        for hint in normalized.get("unresolved_related_concept_hints", []):
            for evidence_id in hint.get("evidence_ids", []):
                evidence = evidence_by_id.get(evidence_id)
                candidate = self._evidence_candidate(hint, evidence, len(candidates) + 1)
                if candidate:
                    candidates.append(candidate)
        return candidates

    def _maintenance_summary(self, clusters: list[dict[str, Any]], invalid_candidates: list[dict[str, Any]]) -> dict[str, Any]:
        promotion_candidates = []
        invalid_promotions = []
        relation_candidates = []
        for cluster in clusters:
            promotion = cluster.get("promotion")
            if promotion and promotion.get("status") == "candidate" and promotion.get("source_refs"):
                promotion_candidates.append(
                    {
                        "cluster_id": cluster["id"],
                        "representative": cluster.get("representative"),
                        "source_refs": promotion.get("source_refs", []),
                        "reason": promotion.get("reason") or "",
                    }
                )
            elif promotion and promotion.get("status") == "candidate":
                invalid_promotions.append(
                    {
                        "cluster_id": cluster["id"],
                        "representative": cluster.get("representative"),
                        "reason": "promotion candidate has no source_refs",
                    }
                )
            for relation in cluster.get("core_relation_candidates", []):
                relation_candidates.append(
                    {
                        "cluster_id": cluster["id"],
                        "target": relation.get("target"),
                        "relation": relation.get("relation"),
                        "evidence": relation.get("evidence", []),
                    }
                )
        return {
            "promotion_candidate_count": len(promotion_candidates),
            "promotion_candidates": promotion_candidates,
            "invalid_candidate_count": len(invalid_candidates),
            "invalid_candidates": invalid_candidates,
            "invalid_promotion_count": len(invalid_promotions),
            "invalid_promotions": invalid_promotions,
            "relation_candidate_count": len(relation_candidates),
            "relation_candidates": relation_candidates,
            "lint_action_available": bool(promotion_candidates or relation_candidates or invalid_candidates or invalid_promotions),
        }

    def _clusters(
        self,
        normalized: dict[str, Any],
        cluster_decisions: list[dict[str, Any]],
        concept_update_decisions: list[dict[str, Any]],
        core_relation_decisions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        clusters: dict[str, dict[str, Any]] = {}
        decisions_by_candidate = {
            str(item.get("candidate_id")): item
            for item in cluster_decisions
            if item.get("candidate_id")
        }
        concept_update_candidate_ids = {
            str(item.get("candidate_id"))
            for item in concept_update_decisions
            if item.get("decision") == "same_concept" and item.get("candidate_id")
        }
        relation_by_candidate = {
            str(item.get("candidate_id")): item
            for item in core_relation_decisions
            if item.get("candidate_id")
        }

        for candidate in self.candidate_claims(normalized):
            if candidate["candidate_id"] in concept_update_candidate_ids:
                continue
            decision = decisions_by_candidate.get(candidate["candidate_id"], {})
            self._add_candidate_claim(clusters, candidate, decision, relation_by_candidate.get(candidate["candidate_id"]))

        return sorted(clusters.values(), key=lambda item: item["id"])

    def _source_candidate(
        self,
        normalized: dict[str, Any],
        item: dict[str, Any],
        candidate_type: str,
        index: int,
    ) -> dict[str, Any] | None:
        term = str(item.get("term") or item.get("title") or item.get("name") or item.get("slug") or "").strip()
        slug = slugify(str(item.get("slug") or term))
        if not term or slug == "untitled":
            return None
        context = str(item.get("context") or "").strip()
        if not context:
            return None
        source_document_id = normalized.get("document", {}).get("document_id")
        refs = global_refs(source_document_id, item.get("anchor_reference_ids", []))
        return {
            "candidate_id": f"cand_{index:03d}",
            "claim_id": f"claim_{_id_fragment(str(source_document_id or 'doc'))}_{index:03d}",
            "term": term,
            "slug": slug,
            "claim": f"{term} - {context}",
            "refs": refs,
            "candidate_type": candidate_type,
            "suggested_promotion_status": "none",
            "suggested_promotion_reason": "",
        }

    def _evidence_candidate(self, hint: dict[str, Any], evidence: dict[str, Any] | None, index: int) -> dict[str, Any] | None:
        if not evidence or not evidence.get("claim"):
            return None
        slug = slugify(str(hint.get("canonical_slug") or hint.get("hint_slug") or "unresolved"))
        if not slug or slug == "untitled":
            return None
        return {
            "candidate_id": f"cand_{index:03d}",
            "claim_id": str(evidence.get("evidence_id") or f"ev_{index:04d}"),
            "term": _title_from_slug(slug),
            "slug": slug,
            "claim": evidence.get("claim") or "",
            "refs": global_refs(evidence.get("source_document_id"), evidence.get("anchor_reference_ids", [])),
            "candidate_type": "evidence",
            "suggested_promotion_status": "none",
            "suggested_promotion_reason": "",
        }

    def _add_candidate_claim(
        self,
        clusters: dict[str, dict[str, Any]],
        candidate: dict[str, Any],
        decision: dict[str, Any],
        relation_decision: dict[str, Any] | None = None,
    ) -> None:
        decision_type = str(decision.get("decision") or "new_cluster")
        target_cluster_id = slugify(str(decision.get("target_cluster_id") or candidate["slug"]))
        if decision_type not in {"same_cluster", "new_cluster", "needs_review"}:
            decision_type = "new_cluster"
        if not target_cluster_id or target_cluster_id == "untitled" or re.fullmatch(r"cluster-\d+", target_cluster_id):
            target_cluster_id = candidate["slug"]
        representative = str(decision.get("representative") or candidate["term"]).strip()
        cluster = self._cluster(clusters, target_cluster_id, representative)
        cluster["evidence_claims"].append(
            {
                "id": candidate["claim_id"],
                "claim": candidate["claim"],
                "refs": candidate["refs"],
                "candidate_type": candidate["candidate_type"],
                "cluster_decision": decision_type,
                "decision_reason": decision.get("reason") or "",
            }
        )
        if relation_decision:
            relation = str(relation_decision.get("relation") or "")
            concept_slug = str(relation_decision.get("concept_slug") or "")
            if relation and concept_slug:
                cluster["core_relation_candidates"].append(
                    {
                        "target": f"concept:{concept_slug}",
                        "relation": relation,
                        "evidence": [candidate["claim_id"]],
                        "reason": relation_decision.get("reason") or "",
                    }
                )
        promotion_status = str(decision.get("promotion_status") or candidate.get("suggested_promotion_status") or "none")
        if promotion_status in {"candidate", "needs_review"}:
            cluster["promotion"] = {
                "status": promotion_status,
                "source_refs": self._source_refs(cluster),
                "reason": decision.get("reason") or candidate.get("suggested_promotion_reason") or "LLM cluster judge 판단",
            }

    def _cluster(self, clusters: dict[str, dict[str, Any]], slug: str, representative: str) -> dict[str, Any]:
        cluster = clusters.get(slug)
        if cluster is None:
            cluster = {
                "id": slug,
                "type": "term_cluster",
                "representative": representative,
                "evidence_claims": [],
                "core_relation_candidates": [],
                "promotion": None,
            }
            clusters[slug] = cluster
        return cluster

    def _source_refs(self, cluster: dict[str, Any]) -> list[str]:
        refs = []
        for claim in cluster.get("evidence_claims", []):
            for ref in claim.get("refs", []):
                document_id, _sep, _block_id = str(ref).partition(":")
                if document_id:
                    refs.append(document_id)
        return unique_keep_order(refs)

    def _active_markdown(self, clusters: list[dict[str, Any]]) -> str:
        if not clusters:
            return "# Active Meaning Clusters\n\n- active cluster 없음\n"
        sections = ["# Active Meaning Clusters"]
        for cluster in clusters:
            sections.append(self._cluster_section(cluster))
        return "\n\n".join(sections) + "\n"

    def _cluster_section(self, cluster: dict[str, Any]) -> str:
        lines = [
            f"## cluster: {cluster['id']}",
            "",
            f"type: {cluster['type']}",
            f"representative: {cluster['representative']}",
            "",
            "### Evidence Claims",
        ]
        lines.extend(
            f"- {claim['id']}: {claim['claim']}{cite_global_refs(claim.get('refs', []))}"
            for claim in cluster.get("evidence_claims", [])
        )
        if not cluster.get("evidence_claims"):
            lines.append("- evidence claim 없음")

        if cluster.get("core_relation_candidates"):
            lines.extend(["", "### Core Relation Candidates"])
            for relation in cluster["core_relation_candidates"]:
                lines.extend(
                    [
                        f"- target: {relation.get('target')}",
                        f"  relation: {relation.get('relation')}",
                        f"  evidence: [{', '.join(relation.get('evidence', []))}]",
                        f"  reason: {relation.get('reason') or '-'}",
                    ]
                )

        promotion = cluster.get("promotion")
        if promotion:
            lines.extend(
                [
                    "",
                    "### Promotion",
                    f"status: {promotion.get('status')}",
                    f"source_refs: [{', '.join(promotion.get('source_refs', []))}]",
                    f"reason: {promotion.get('reason') or '-'}",
                ]
            )
        return "\n".join(lines)

def _title_from_slug(slug: str) -> str:
    return " ".join(part for part in slug.split("-") if part) or slug


def _id_fragment(value: str) -> str:
    return sha1(value.encode("utf-8")).hexdigest()[:8]
