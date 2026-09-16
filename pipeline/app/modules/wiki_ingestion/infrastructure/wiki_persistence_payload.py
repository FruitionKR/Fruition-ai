from pathlib import Path
from typing import Any


def source_summary(normalized: dict[str, Any], manifest: dict[str, Any] | None = None) -> str:
    manifest = manifest or {}
    artifact = manifest.get("source_extraction_artifact")
    if isinstance(artifact, dict) and artifact.get("summary"):
        return str(artifact["summary"])
    source_page = manifest.get("source_page")
    if isinstance(source_page, dict):
        source_artifact = source_page.get("source_extraction_artifact")
        if isinstance(source_artifact, dict) and source_artifact.get("summary"):
            return str(source_artifact["summary"])
    for note in normalized.get("semantic_notes", []):
        summary = note.get("semantic_summary")
        if summary:
            return summary
    return normalized.get("document", {}).get("title", "")


def markdown_title(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def page_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        if "markdown" not in value:
            raise RuntimeError("Pipeline manifest page payload is missing markdown")
        return value
    path = Path(str(value))
    if not path.exists():
        raise RuntimeError(f"Pipeline manifest page path does not exist: {path}")
    markdown = path.read_text(encoding="utf-8")
    return {
        "slug": path.stem,
        "title": markdown_title(markdown),
        "markdown_path": str(path),
        "markdown": markdown,
    }


def stored_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    stored = dict(manifest)
    source_page = stored.get("source_page")
    if isinstance(source_page, dict):
        stored["source_page"] = stored_page(source_page)
    stored["concept_pages"] = [
        stored_page(page) if isinstance(page, dict) else page
        for page in stored.get("concept_pages", [])
    ]
    stored.pop("normalized", None)
    stored.pop("source_blocks", None)
    stored.pop("concept_contributions", None)
    meaning_clusters = stored.get("meaning_clusters")
    if isinstance(meaning_clusters, dict):
        stored["meaning_clusters"] = {
            key: value
            for key, value in meaning_clusters.items()
            if key not in {"active_markdown", "log_markdown", "clusters"}
        }
    return stored


def stored_page(page: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in page.items()
        if key not in {"markdown", "source_extraction_artifact"}
    }


def resolve_page_id(value: str | None, source_page_id: str, concept_id_by_slug: dict[str, str]) -> str | None:
    if not value:
        return None
    if value.startswith("source:"):
        return source_page_id
    if value.startswith("concept:"):
        return concept_id_by_slug.get(value.split(":", 1)[1])
    return None
