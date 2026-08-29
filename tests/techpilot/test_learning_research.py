"""S1-C evidence, local-text ingestion, and recoverable task tests."""

from __future__ import annotations

import pytest

from techpilot.learning.contracts import DocumentRecord, LearningTask, SourceRecord, TrendBrief
from techpilot.learning.research import FetchedSource, ResearchError, ResearchService
from techpilot.learning.service import LearningService
from techpilot.learning.store import LearningStore


class FakeConnector:
    def __init__(self, source: FetchedSource | None = None, error: Exception | None = None) -> None:
        self.source = source
        self.error = error
        self.calls: list[str] = []

    def read(self, uri: str, *, timeout: int = 15) -> FetchedSource:
        self.calls.append(uri)
        if self.error is not None:
            raise self.error
        assert self.source is not None
        return self.source


def _goal(store: LearningStore):
    return LearningService(store).confirm(
        LearningService.draft_from_command("FastAPI"),
        baseline_notes=None,
    ).goal


def test_research_url_persists_evidence_task_and_version_watchlist(tmp_path):
    store = LearningStore(tmp_path / "learning")
    goal = _goal(store)
    connector = FakeConnector(FetchedSource(
        uri="https://example.test/fastapi-release",
        title="FastAPI release notes",
        text="FastAPI 1.0 introduces a documented migration path.",
        content_type="text/html",
        version="2026-08-01",
        source_type="web",
    ))

    result = ResearchService(store=store, connector=connector).research_url("https://example.test/fastapi-release")

    assert connector.calls == ["https://example.test/fastapi-release"]
    assert result.source is not None
    assert result.source.goal_id == goal.id
    assert result.source.summary == "FastAPI 1.0 introduces a documented migration path."
    assert result.task is not None
    assert result.task.source_ids == (result.source.id,)
    assert result.task.acceptance_criteria
    assert result.trend is not None
    assert result.trend.category == "watchlist"
    assert result.trend.source_ids == (result.source.id,)
    assert len(store.list_records(SourceRecord)) == 1
    assert len(store.list_records(LearningTask)) == 1
    assert len(store.list_records(TrendBrief)) == 1


def test_research_replaces_same_url_when_content_changes_without_duplicate_task(tmp_path):
    store = LearningStore(tmp_path / "learning")
    _goal(store)
    connector = FakeConnector(FetchedSource(
        uri="https://example.test/source",
        title="Official documentation",
        text="Version one",
    ))
    service = ResearchService(store=store, connector=connector)

    first = service.research_url("https://example.test/source")
    connector.source = FetchedSource(
        uri="https://example.test/source",
        title="Official documentation",
        text="Version two",
    )
    second = service.research_url("https://example.test/source")

    assert first.source is not None and second.source is not None
    assert first.source.id == second.source.id
    assert second.updated
    assert len(store.list_records(SourceRecord)) == 1
    assert len(store.list_records(LearningTask)) == 1


def test_failed_research_creates_no_source_or_task(tmp_path):
    store = LearningStore(tmp_path / "learning")
    _goal(store)
    service = ResearchService(store=store, connector=FakeConnector(error=ResearchError("network refused")))

    with pytest.raises(ResearchError, match="network refused"):
        service.research_url("https://example.test/unavailable")

    assert store.list_records(SourceRecord) == []
    assert store.list_records(LearningTask) == []


def test_native_text_document_is_extracted_and_added_as_source(tmp_path):
    store = LearningStore(tmp_path / "learning")
    _goal(store)
    document = tmp_path / "fastapi-notes.md"
    document.write_text("# FastAPI\n\nUse type hints for request validation.", encoding="utf-8")

    result = ResearchService(store=store).ingest_document(document)

    assert result.document is not None
    assert result.document.extraction_status == "extracted"
    assert result.extracted is not None
    assert "request validation" in result.extracted.text
    assert result.source is not None
    assert result.source.source_type == "document"
    assert len(store.list_records(DocumentRecord)) == 1


def test_binary_or_scanned_document_is_explicitly_unsupported(tmp_path):
    store = LearningStore(tmp_path / "learning")
    _goal(store)
    document = tmp_path / "scan.pdf"
    document.write_bytes(b"not a parsed PDF")

    result = ResearchService(store=store).ingest_document(document)

    assert result.document is not None
    assert result.document.extraction_status == "unsupported"
    assert result.source is None
    assert "暂不支持" in result.message
