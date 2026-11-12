from app.modules.mcp_wiki_draft.preview_summary import UNTITLED_PAGE_TITLE, summary_item


def test_blank_page_title_falls_back_so_document_does_not_reject_the_draft():
    for title in ("", "   ", None):
        assert summary_item("SOURCE_PAGE", None, title)["page_title"] == UNTITLED_PAGE_TITLE
    assert summary_item("SOURCE_PAGE", None, " 회의록 ")["page_title"] == "회의록"
