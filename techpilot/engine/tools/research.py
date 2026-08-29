"""Runtime tools that hand successfully read learning sources to S1-C storage."""

from __future__ import annotations

from .base import Tool


class ResearchUrlTool(Tool):
    """Read a public URL only after the Runtime grants network permission."""

    name = "research_url"
    description = (
        "Read a user-provided public URL or GitHub repository for the active learning path. "
        "Use only when the learner explicitly asks to research, read, or add that source. "
        "It records the retrieved source and one study task; never invents a source when reading fails."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The public http(s) URL to research"},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 15)"},
        },
        "required": ["url"],
    }

    def execute(self, url: str, timeout: int = 15) -> str:
        from techpilot.learning.research import ResearchError, ResearchService

        try:
            return ResearchService().research_url(url, timeout=timeout).message
        except ResearchError as error:
            return f"研究资料失败：{error}"


class ResearchDocumentTool(Tool):
    """Extract a supported, user-selected local text document into learning data."""

    name = "research_document"
    description = (
        "Read a user-selected local text document for the active learning path. "
        "Use only when the learner explicitly asks to add that file as study material. "
        "Supports native text formats; do not use for images or scanned PDFs."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "A file inside the current workspace"},
        },
        "required": ["file_path"],
    }

    def execute(self, file_path: str) -> str:
        from techpilot.learning.research import ResearchError, ResearchService

        try:
            return ResearchService().ingest_document(file_path).message
        except ResearchError as error:
            return f"读取学习资料失败：{error}"
