from app.modules.query.application.ports import WikiMarkdownReaderPort
from app.modules.wiki_ingestion.infrastructure.object_storage import read_text_object


class MinioWikiMarkdownReader(WikiMarkdownReaderPort):
    def read_markdown(self, markdown_uri: str) -> str:
        return read_text_object(markdown_uri)
