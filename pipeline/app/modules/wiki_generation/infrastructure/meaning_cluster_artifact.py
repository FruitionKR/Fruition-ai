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
        promotions: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        concept_update_decisions = concept_update_decisions or []
        clusters = self._clusters(normalized, cluster_decisions or [], concept_update_decisions)
        self._apply_promotions(clusters, promotions or {})
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
            "active_path": f"wiki/{workspace_id}/clusters/active.md",
            "log_path": f"wiki/{workspace_id}/logs/{ingest_date}.md",
            "active_markdown": active_markdown,
            "log_markdown": log_markdown,
            "clusters": clusters,
            "concept_update_decisions": concept_update_decisions,
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
    ) -> dict[str, Any]:
        artifact = self.build(
            normalized,
            user_id=user_id,
            workspace_id=workspace_id,
            cluster_decisions=cluster_decisions,
            concept_update_decisions=concept_update_decisions,
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
        # 후보 번호는 하위 항목 배정 전에 매긴 개념 갱신 판정과 맞아야 한다. 배정받아 빼는 섹션·언급도 번호는 쓴다.
        count = 0
        for kind, field in (("section", "section_candidates"), ("mention", "mentions")):
            for item in normalized.get(field, []):
                candidate = self._source_candidate(normalized, item, kind, count + 1)
                if candidate:
                    count += 1
                    # 하위 항목으로 배정받은 섹션·언급은 이미 개념이 됐다. 다시 모아 승격하지 않는다(ADR 0042).
                    if not item.get("assigned_entry"):
                        candidates.append(candidate)
        for hint in normalized.get("unresolved_related_concept_hints", []):
            for evidence_id in hint.get("evidence_ids", []):
                evidence = evidence_by_id.get(evidence_id)
                candidate = self._evidence_candidate(hint, evidence, count + 1)
                if candidate:
                    count += 1
                    candidates.append(candidate)
        return candidates

    def _maintenance_summary(self, clusters: list[dict[str, Any]], invalid_candidates: list[dict[str, Any]]) -> dict[str, Any]:
        promotion_candidates = []
        invalid_promotions = []
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
        return {
            "promotion_candidate_count": len(promotion_candidates),
            "promotion_candidates": promotion_candidates,
            "invalid_candidate_count": len(invalid_candidates),
            "invalid_candidates": invalid_candidates,
            "invalid_promotion_count": len(invalid_promotions),
            "invalid_promotions": invalid_promotions,
            "lint_action_available": bool(promotion_candidates or invalid_candidates or invalid_promotions),
        }

    def _clusters(
        self,
        normalized: dict[str, Any],
        cluster_decisions: list[dict[str, Any]],
        concept_update_decisions: list[dict[str, Any]],
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

        for candidate in self.candidate_claims(normalized):
            if candidate["candidate_id"] in concept_update_candidate_ids:
                continue
            decision = decisions_by_candidate.get(candidate["candidate_id"], {})
            self._add_candidate_claim(clusters, candidate, decision)

        return sorted(clusters.values(), key=lambda item: item["id"])

    def _apply_promotions(self, clusters: list[dict[str, Any]], promotions: dict[str, dict[str, Any]]) -> None:
        """승격 판정을 받은 클러스터에 승격 표시를 붙인다. 이번 편입이 근거를 더하지 않은 기존 클러스터는 표시만 담는다."""
        by_id = {cluster["id"]: cluster for cluster in clusters}
        for cluster_id, promotion in promotions.items():
            cluster = by_id.get(cluster_id)
            if cluster is None:
                cluster = self._cluster(by_id, cluster_id, str(promotion.get("representative") or _title_from_slug(cluster_id)))
                clusters.append(cluster)
            cluster["promotion"] = {
                "status": "candidate",
                "source_refs": self._source_refs(cluster) or list(promotion.get("source_refs") or []),
                "reason": promotion.get("reason") or "",
                "target": promotion.get("target") or "",
            }
        clusters.sort(key=lambda item: item["id"])

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
        # 언급은 설명 없이 이름만 받으므로 언급이 나온 원문으로 판정한다.
        context = str(item.get("context") or item.get("definition") or "").strip()
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
        }

    def _add_candidate_claim(
        self,
        clusters: dict[str, dict[str, Any]],
        candidate: dict[str, Any],
        decision: dict[str, Any],
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

    def _cluster(self, clusters: dict[str, dict[str, Any]], slug: str, representative: str) -> dict[str, Any]:
        cluster = clusters.get(slug)
        if cluster is None:
            cluster = {
                "id": slug,
                "type": "term_cluster",
                "representative": representative,
                "evidence_claims": [],
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
            if promotion.get("target"):
                lines.append(f"target: concept:{promotion['target']}")
        return "\n".join(lines)

def _title_from_slug(slug: str) -> str:
    return " ".join(part for part in slug.split("-") if part) or slug


def _id_fragment(value: str) -> str:
    return sha1(value.encode("utf-8")).hexdigest()[:8]
