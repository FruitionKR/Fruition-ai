from app.modules.wiki_ingestion.domain.unit_text import clean_unit_text


def test_removes_list_marker_metadata_and_source_references() -> None:
    text = "- [claim_001] 역기전력은 속도에 비례한다 [source:B0001, B0002]"

    assert clean_unit_text(text) == "역기전력은 속도에 비례한다"
